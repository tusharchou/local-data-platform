"""The ``ldp mcp`` command: serve Iceberg tables to an agent over MCP stdio.

::

    ldp mcp --config DIR_OR_FILE [--config ...] [--catalog SPEC.json] [--allow TABLES]
            [--max-rows N] [--timeout SECONDS] [--audit PATH] [--no-iceberg-audit] [--no-native]
            [--install-extensions]

:func:`add_cli` registers the command on the ``ldp`` parser; ``python -m
local_data_platform.mcp_server`` runs the same command on its own.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from typing import Any

from local_data_platform.exceptions import ConfigError, LDPError

from .tools import DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT_S

COMMAND = "mcp"


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


def add_arguments(parser: argparse.ArgumentParser, *, verbose: bool = True) -> argparse.ArgumentParser:
    """Add the ``ldp mcp`` options to ``parser`` and set its handler.

    Args:
        parser: The parser to add to.
        verbose: Also add ``-v``/``--verbose``. Off when a parent parser already supplies it.
    """
    parser.add_argument("--config", action="append", default=[], metavar="DIR_OR_FILE",
                        help="a dataset config, or a folder of *.json configs; every Iceberg source and target is "
                             "served (repeatable)")
    parser.add_argument("--catalog", action="append", default=[], metavar="SPEC.json",
                        help="a catalog spec file; every table in the catalog is served (repeatable)")
    parser.add_argument("--allow", action="append", metavar="TABLES",
                        help="comma-separated table allowlist, e.g. 'demo.rides,demo.*' (default: every table "
                             "found, except the _ldp system tables)")
    parser.add_argument("--max-rows", type=_positive_int, default=DEFAULT_MAX_ROWS,
                        help=f"row cap for every result (default {DEFAULT_MAX_ROWS})")
    parser.add_argument("--timeout", type=_positive_float, default=DEFAULT_TIMEOUT_S, metavar="SECONDS",
                        help=f"stop a query after this many seconds (default {DEFAULT_TIMEOUT_S:g})")
    parser.add_argument("--audit", metavar="PATH",
                        help="JSONL audit log (default <warehouse>/.ldp/audit/mcp_audit.jsonl)")
    parser.add_argument("--no-iceberg-audit", action="store_true",
                        help="audit to the JSONL file only; by default the records are also appended to the "
                             "_ldp.audit Iceberg table of the first served catalog when the server stops")
    parser.add_argument("--no-native", action="store_true",
                        help="serve tables as in-memory Arrow instead of DuckDB's iceberg_scan")
    parser.add_argument("--install-extensions", action="store_true",
                        help="install the DuckDB iceberg extension if missing (needs network once)")
    if verbose:
        parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                            help="debug logging (on stderr), and a full traceback on errors")
    parser.set_defaults(handler=run_command)
    return parser


def add_cli(subparsers: Any, parents: Sequence[argparse.ArgumentParser] = ()) -> argparse.ArgumentParser:
    """Register ``ldp mcp`` on the ``ldp`` sub-command parsers.

    Args:
        subparsers: The object returned by ``ArgumentParser.add_subparsers()``.
        parents: Parent parsers for shared options. When given, they must supply ``-v``, as the
            ``ldp`` CLI's shared parser does; without parents the command adds its own ``-v``.

    Returns:
        The ``mcp`` parser. Its ``handler`` default runs the server and returns the exit status.
    """
    parser = subparsers.add_parser(
        COMMAND, parents=list(parents),
        help="serve Iceberg tables read-only to AI agents over MCP (stdio)",
        description="Run a read-only Model Context Protocol server over stdio. It exposes list_tables, "
                    "describe_table, query, sample_rows, table_history and get_dataset, runs SQL in a locked-down "
                    "DuckDB, and audits every call. Needs the mcp and duckdb extras: "
                    'pip install "local-data-platform[mcp,duckdb]".')
    return add_arguments(parser, verbose=not parents)


def run_command(args: argparse.Namespace) -> int:
    """Build the tools from ``args`` and serve them over stdio until the client disconnects."""
    from .server import import_mcp, serve_stdio
    from .tools import LakeTools

    import_mcp()
    if not args.config and not args.catalog:
        raise ConfigError("ldp mcp needs at least one --config or --catalog")
    tools = LakeTools.from_sources(args.config, args.catalog, allow=args.allow, audit_path=args.audit,
                                   iceberg_audit=not args.no_iceberg_audit,
                                   max_rows=args.max_rows, timeout_s=args.timeout,
                                   native=False if args.no_native else None,
                                   install_extensions=args.install_extensions)
    try:
        if not tools.tables:
            reasons = "; ".join(f"{item['table']}: {item['reason']}" for item in tools.unavailable) or "none found"
            raise ConfigError(f"no tables to serve ({reasons})")
        print(f"ldp mcp: serving {len(tools.tables)} tables over stdio ({', '.join(tools.tables)}); "
              f"audit log {tools.audit.path}", file=sys.stderr)
        serve_stdio(tools)
    finally:
        tools.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``ldp mcp`` as ``python -m local_data_platform.mcp_server``.

    Returns:
        0 when the client disconnects, 1 on error.
    """
    from local_data_platform.logger import configure_cli_logging

    parser = add_arguments(argparse.ArgumentParser(prog="python -m local_data_platform.mcp_server",
                                                   description="Serve Iceberg tables read-only over MCP stdio."))
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return 0 if exit_.code in (0, None) else 1
    verbose = bool(getattr(args, "verbose", False))
    configure_cli_logging(logging.DEBUG if verbose else logging.WARNING)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        return 130
    except LDPError as error:
        print(f"ldp mcp: error: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - one line on stderr, like the ldp CLI
        if verbose:
            raise
        print(f"ldp mcp: error: {type(error).__name__}: {error} (run with -v for the traceback)", file=sys.stderr)
        return 1


__all__ = ["COMMAND", "add_arguments", "add_cli", "main", "run_command"]
