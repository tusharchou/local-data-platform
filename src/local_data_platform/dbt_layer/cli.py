"""The ``ldp dbt`` command: run a dbt project over LDP's Iceberg tables.

    ldp dbt init PROJECT_DIR --sources CONFIG_DIR --warehouse WAREHOUSE_DIR
    ldp dbt build [OPTIONS] PROJECT_DIR [DBT_ARGS ...]
    ldp dbt test  [OPTIONS] PROJECT_DIR [DBT_ARGS ...]
    ldp dbt docs  [OPTIONS] PROJECT_DIR [DBT_ARGS ...]
    ldp dbt exec  [OPTIONS] PROJECT_DIR DBT_COMMAND [DBT_ARGS ...]

``build``, ``test``, ``docs`` and ``exec`` register LDP's Iceberg tables as dbt sources first, then run
dbt. Only ``build`` publishes: it writes the models marked ``ldp_publish`` back to Iceberg once dbt
has succeeded. Everything after ``PROJECT_DIR`` goes to dbt unchanged. The contract is
``docs/design/warehousing.md``.

``cli.py`` wires this in with :func:`add_cli`. dbt is a subprocess, so nothing here imports it.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from typing import Any

from local_data_platform.dbt_layer.project import (
    INSTALL_HINT,
    DbtProject,
    check_profile,
    find_dbt,
    find_target_path,
    init_project,
    run_dbt,
)
from local_data_platform.dbt_layer.publish import (
    Published,
    PublishOutcome,
    failed_nodes,
    plan_publish,
    publish,
    read_artifacts,
)
from local_data_platform.dbt_layer.sources import MODES, SourceRegistration, register_sources
from local_data_platform.exceptions import ConfigError, LDPError

DOCS_HOST = "127.0.0.1"
DEFAULT_DOCS_PORT = 8080
MAX_FAILED_LINES = 10
CLOCK_SLACK_S = 2.0
"""How much older than the start of a dbt run an artifact may be and still count as written by it."""


def _out(text: str = "") -> None:
    print(text, flush=True)


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a port number, got {text!r}") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"expected a port from 1 to 65535, got {value}")
    return value


# --------------------------------------------------------------------------- shared steps


def _dbt_arguments(args: argparse.Namespace) -> list[str]:
    """What the user wants dbt to get: everything after ``PROJECT_DIR``, without a leading ``--``."""
    given = list(args.dbt_args)
    return given[1:] if given[:1] == ["--"] else given


def _prepare(args: argparse.Namespace) -> tuple[DbtProject, str]:
    """Load the project, find dbt, and register the sources, in that order, so a missing dbt changes nothing."""
    project = DbtProject.load(args.project_dir)
    binary = find_dbt(args.dbt_bin)
    check_profile(project)
    if not args.no_sources:
        _report_sources(register_sources(project.database, project.source_configs, project.source_catalogs,
                                         mode=args.sources_mode, sources_file=project.sources_file))
    return project, binary


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _report_sources(registration: SourceRegistration) -> None:
    for item in registration.unavailable:
        print(f"ldp: warning: {item['table']} is not a source yet: {item['reason']}", file=sys.stderr)
    views = sum(source.kind == "view" for source in registration.sources)
    copies = len(registration.sources) - views
    total = _plural(len(registration.sources), "source", "sources")
    if not registration.sources:
        summary = "no sources (no Iceberg tables were found)"
    elif not copies:
        summary = f"{total} as views over Iceberg snapshots"
    elif not views:
        summary = f"{total} as copies of Iceberg snapshots"
    else:
        summary = f"{total} ({_plural(views, 'view', 'views')}, {_plural(copies, 'copy', 'copies')})"
    dropped = f", removed {len(registration.dropped)} that no longer exist" if registration.dropped else ""
    _out(f"ldp: registered {summary}{dropped}")


def _run_plain(args: argparse.Namespace, command: list[str]) -> int:
    """Run a dbt command that publishes nothing and map a failure to exit status 1."""
    project, binary = _prepare(args)
    status = run_dbt(project, binary, [*command, *_dbt_arguments(args)])
    if status != 0:
        raise LDPError(f"dbt {command[0]} exited with status {status}")
    return 0


# --------------------------------------------------------------------------- commands


def _cmd_init(args: argparse.Namespace) -> int:
    written = init_project(args.project_dir, args.sources, args.warehouse)
    folder = written[0].parent
    _out(f"ldp: created a dbt project in {folder}")
    for path in written:
        _out(f"  {path.relative_to(folder).as_posix()}")
    _out(f"Next: ldp dbt build {args.project_dir}")
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    project, binary = _prepare(args)
    extra = _dbt_arguments(args)
    started = time.time() - CLOCK_SLACK_S
    status = run_dbt(project, binary, ["build", *extra])
    target = find_target_path(project, extra)
    if status != 0:
        raise _dbt_failed(project, target, started, status)
    manifest, run_results = read_artifacts(project, target, since=started)
    plan = plan_publish(project, manifest, run_results)
    if not plan:
        _out("ldp: no model marked ldp_publish: true ran in this build, so nothing was published")
        return 0
    outcome = publish(plan, report=_report_published)
    if outcome.failed:
        raise _publish_failed(outcome)
    return 0


def _cmd_test(args: argparse.Namespace) -> int:
    return _run_plain(args, ["test"])


def _cmd_exec(args: argparse.Namespace) -> int:
    if not _dbt_arguments(args):
        raise ConfigError("exec needs a dbt command to run, such as: ldp dbt exec PROJECT_DIR ls")
    project, binary = _prepare(args)
    status = run_dbt(project, binary, _dbt_arguments(args))
    if status != 0:
        raise LDPError(f"dbt exited with status {status}")
    return 0


def _cmd_docs(args: argparse.Namespace) -> int:
    project, binary = _prepare(args)
    status = run_dbt(project, binary, ["docs", "generate", *_dbt_arguments(args)])
    if status != 0:
        raise LDPError(f"dbt docs generate exited with status {status}")
    if not args.serve:
        return 0
    _out(f"ldp: serving the docs on http://{DOCS_HOST}:{args.port}, stop with Ctrl-C")
    try:
        status = run_dbt(project, binary, ["docs", "serve", "--host", DOCS_HOST, "--port", str(args.port)])
    except KeyboardInterrupt:
        return 0
    if status != 0:
        raise LDPError(f"dbt docs serve exited with status {status}")
    return 0


# --------------------------------------------------------------------------- reporting


def _report_published(item: Published) -> None:
    _out(f"ldp: published {item.table}: {item.mode}, {item.rows_before} -> {item.rows_after} rows, "
         f"snapshot {item.snapshot_id}")
    for line in item.quality:
        _out(f"  {line}")


def _dbt_failed(project: DbtProject, target: str, started: float, status: int) -> LDPError:
    """The error for a dbt build that exited with ``status``, naming the nodes that failed when dbt wrote them."""
    lines = [f"dbt build failed with exit status {status}, so nothing was published"]
    try:
        _, run_results = read_artifacts(project, target, since=started)
    except (ConfigError, ValueError, OSError):
        return LDPError("\n".join(lines))
    failed = failed_nodes(run_results)
    for node in failed[:MAX_FAILED_LINES]:
        lines.append(f"  - {node['node']}: {node['status']}" + (f", {node['message']}" if node["message"] else ""))
    if len(failed) > MAX_FAILED_LINES:
        lines.append(f"  ... and {len(failed) - MAX_FAILED_LINES} more")
    return LDPError("\n".join(lines))


def _publish_failed(outcome: PublishOutcome) -> LDPError:
    model, error = outcome.failed
    detail = str(error) if isinstance(error, LDPError) else f"{type(error).__name__}: {error}"
    lines = [f"publishing {model} failed, so the models after it were not tried"]
    lines += [f"  {line}" for line in detail.splitlines()]
    if outcome.published:
        lines.append("  published: " + ", ".join(item.table for item in outcome.published))
    lines.append("  not published: " + ", ".join([model, *(outcome.pending or [])]))
    lines.append(f"  to retry {model} by hand: ldp run {outcome.failed_config}")
    return LDPError("\n".join(lines))


# --------------------------------------------------------------------------- argparse


def _add_project_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dbt-bin", metavar="PATH",
                        help="the dbt to run (default: the one next to this Python, else the one on PATH)")
    parser.add_argument("--sources-mode", choices=MODES, default="auto",
                        help="how to register Iceberg tables as sources: views over the tables, copies of them, "
                             "or views when DuckDB's iceberg extension is installed and copies if not (default auto)")
    parser.add_argument("--no-sources", action="store_true",
                        help="do not register sources, for a dbt command that does not read them, such as clean")
    parser.add_argument("project_dir", metavar="PROJECT_DIR", help="the dbt project folder, with its ldp_dbt.json")
    parser.add_argument("dbt_args", metavar="DBT_ARGS", nargs=argparse.REMAINDER,
                        help="passed to dbt unchanged, so options of ldp dbt go before PROJECT_DIR")


def add_cli(subparsers: Any, *, parents: Sequence[argparse.ArgumentParser] = ()) -> argparse.ArgumentParser:
    """Register ``ldp dbt`` on an argparse subparsers object.

    Args:
        subparsers: The object ``ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers to inherit options from (``cli.py``'s shared ``-v``).

    Returns:
        The ``dbt`` parser. Each of its commands has a ``handler`` default that takes the parsed args
        and returns the exit status.
    """
    dbt = subparsers.add_parser(
        "dbt", parents=list(parents), help="run a dbt project over LDP tables and publish models to Iceberg",
        description="Register LDP's Iceberg tables as dbt sources, run dbt, and write the models marked "
                    f"ldp_publish back to Iceberg. Needs the dbt extra: {INSTALL_HINT}.")
    commands = dbt.add_subparsers(dest="dbt_command", metavar="COMMAND")

    init = commands.add_parser(
        "init", parents=list(parents), help="write a dbt project that is ready to build",
        description="Write a dbt project with a staging model on the first source table and a gold model "
                    "marked ldp_publish. PROJECT_DIR must be empty or missing; nothing is overwritten.")
    init.add_argument("project_dir", metavar="PROJECT_DIR", help="the new project folder")
    init.add_argument("--sources", required=True, metavar="CONFIG_DIR",
                      help="the folder of dataset configs whose Iceberg tables are the sources")
    init.add_argument("--warehouse", required=True, metavar="WAREHOUSE_DIR",
                      help="the folder the published tables are written to")
    init.set_defaults(handler=_cmd_init)

    build = commands.add_parser(
        "build", parents=list(parents), help="run dbt build and publish the models marked ldp_publish",
        description="Register the sources, run dbt build, and when it succeeds write each model marked "
                    "ldp_publish to Iceberg. When dbt fails nothing is published.",
        epilog="Example: ldp dbt build PROJECT_DIR --select +gold_totals")
    _add_project_arguments(build)
    build.set_defaults(handler=_cmd_build)

    test = commands.add_parser(
        "test", parents=list(parents), help="register the sources and run dbt test",
        description="Register the sources and run dbt test. Nothing is published.")
    _add_project_arguments(test)
    test.set_defaults(handler=_cmd_test)

    docs = commands.add_parser(
        "docs", parents=list(parents), help="register the sources and generate the dbt docs",
        description="Register the sources and run dbt docs generate. Nothing is published.")
    docs.add_argument("--serve", action="store_true", help=f"then serve the docs on {DOCS_HOST} until Ctrl-C")
    docs.add_argument("--port", type=_port, default=DEFAULT_DOCS_PORT,
                      help=f"the port for --serve (default {DEFAULT_DOCS_PORT})")
    _add_project_arguments(docs)
    docs.set_defaults(handler=_cmd_docs)

    execute = commands.add_parser(
        "exec", parents=list(parents), help="register the sources and run any dbt command",
        description="Register the sources and run any dbt command on the project. Nothing is published.",
        epilog="Example: ldp dbt exec PROJECT_DIR ls --select staging")
    _add_project_arguments(execute)
    execute.set_defaults(handler=_cmd_exec)

    def _usage(args: argparse.Namespace) -> int:
        dbt.print_help(sys.stderr)
        return 1

    dbt.set_defaults(handler=_usage)
    return dbt


__all__ = ["add_cli"]
