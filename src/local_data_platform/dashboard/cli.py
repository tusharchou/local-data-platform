"""The ``ldp dashboard`` command: a local web page for the lakehouse, served by marimo on 127.0.0.1.

::

    ldp dashboard --config DIR_OR_FILE [--config ...] [--catalog SPEC.json] [--allow TABLES] [--reports DIR]
                  [--port N] [--no-browser] [--edit]

:func:`add_cli` registers the command on the ``ldp`` parser. The command checks that the ``notebook`` extra is installed
and that there are tables to show, so that a mistake is an error in the terminal and not a page in the browser. It then
puts what it was asked for into the environment, as :class:`~local_data_platform.dashboard.service.Settings`, and runs
``marimo run`` on the notebook of the package, ``dashboard/app.py``. The server is told ``--host 127.0.0.1`` and nothing
else, so no option of this command opens it to the network.

This module needs only the standard library when it is imported.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from local_data_platform.exceptions import ConfigError, EngineNotFound

if TYPE_CHECKING:
    from .service import Settings

COMMAND = "dashboard"
HOST = "127.0.0.1"
"""The only address that the server listens on."""
NOTEBOOK_INSTALL_HINT = 'pip install "local-data-platform[notebook]"'
REQUIRED = ("marimo", "altair")
"""The packages of the ``notebook`` extra that the dashboard needs, by their import names."""
NOTEBOOK = Path(__file__).with_name("app.py")
"""The notebook that ``marimo run`` serves."""
EDIT_COPY = "ldp_dashboard.py"
"""The copy of the notebook that ``--edit`` opens, in the folder that the command was run in."""


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a port number, got {text!r}") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"expected a port from 1 to 65535, got {value}")
    return value


def add_arguments(parser: argparse.ArgumentParser, *, verbose: bool = True) -> argparse.ArgumentParser:
    """Add the ``ldp dashboard`` options to ``parser`` and set its handler.

    Args:
        parser: The parser to add to.
        verbose: Also add ``-v``/``--verbose``. Off when a parent parser already supplies it.
    """
    parser.add_argument("--config", action="append", default=[], metavar="PATH",
                        help="a dataset config, or a folder of *.json configs; every Iceberg source and target is "
                             "shown, and the Health page lists the pipelines (repeatable)")
    parser.add_argument("--catalog", action="append", default=[], metavar="SPEC",
                        help="a catalog spec file; every table in the catalog is shown (repeatable)")
    parser.add_argument("--allow", action="append", metavar="TABLES",
                        help="comma-separated table allowlist, as ldp mcp takes it, e.g. 'demo.rides,demo.*' (default: "
                             "every table found, except the _ldp system tables)")
    parser.add_argument("--reports", metavar="DIR",
                        help="a folder of report files, or one report file, for the Reports page")
    parser.add_argument("--port", type=_port, metavar="N", help="the port to listen on (default: marimo's own, 2718)")
    parser.add_argument("--no-browser", action="store_true", dest="no_browser",
                        help="do not open the browser; the address is printed")
    parser.add_argument("--edit", action="store_true",
                        help=f"open the notebook for editing in marimo, as a copy called {EDIT_COPY} in the current "
                             f"folder "
                             "that is made once and never overwritten. The editor can run any Python, so the guard of "
                             "the dashboard does not apply to what is written there")
    if verbose:
        parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                            help="debug logging (on stderr), and a full traceback on errors")
    parser.set_defaults(handler=run_command)
    return parser


def add_cli(subparsers: Any, parents: Sequence[argparse.ArgumentParser] = ()) -> argparse.ArgumentParser:
    """Register ``ldp dashboard`` on the ``ldp`` sub-command parsers.

    Args:
        subparsers: The object returned by ``ArgumentParser.add_subparsers()``.
        parents: Parent parsers for shared options. When given, they must supply ``-v``, as the ``ldp`` CLI's shared
            parser does; without parents the command adds its own ``-v``.

    Returns:
        The ``dashboard`` parser. Its ``handler`` default runs the server and returns the exit status.
    """
    parser = subparsers.add_parser(
        COMMAND, parents=list(parents),
        help="browse the tables, run a query, read a report and see the health of the pipelines in a local web page",
        description="Serve a local web page, on 127.0.0.1 only, with four pages. Tables lists the tables with their "
                    "schema, their first rows and their snapshots. SQL runs one query with the guard, the row cap and "
                    "the timeout of ldp mcp, and shows the result as a table and as a quick chart. Reports builds a "
                    "report of --reports for a window and can write its HTML page. Health shows the rows of ldp "
                    "status. Every query goes through the tools of ldp mcp, and its audit record starts with "
                    "dashboard. The page is a "
                    "marimo notebook. Needs the notebook extra: " + NOTEBOOK_INSTALL_HINT)
    return add_arguments(parser, verbose=not parents)


def missing_packages() -> list[str]:
    """The packages of :data:`REQUIRED` that are not installed."""
    return [name for name in REQUIRED if importlib.util.find_spec(name) is None]


def require_notebook() -> None:
    """Check that the ``notebook`` extra is installed.

    Raises:
        EngineNotFound: If marimo or Altair is not installed. The message has the command that installs them.
    """
    missing = missing_packages()
    if missing:
        raise EngineNotFound(f"ldp dashboard needs {' and '.join(missing)}, which the notebook extra installs. Install "
                             f"it with: {NOTEBOOK_INSTALL_HINT}")


def _absolute(path: str, what: str) -> str:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise ConfigError(f"{what} not found: {resolved}")
    return str(resolved)


def settings_from(args: argparse.Namespace) -> Settings:
    """The :class:`~local_data_platform.dashboard.service.Settings` that the options of the command say.

    Every path is made absolute, so that the notebook finds it whatever its own folder is, and a path that is not
    there is an error.

    Raises:
        ConfigError: If there is no source, or a path is not there.
    """
    from .service import Settings

    if not args.config and not args.catalog:
        raise ConfigError("ldp dashboard needs at least one --config or --catalog")
    return Settings(configs=tuple(_absolute(item, "config") for item in args.config),
                    catalogs=tuple(_absolute(item, "catalog spec") for item in args.catalog),
                    allow=tuple(item for item in (args.allow or []) if item.strip()),
                    reports=_absolute(args.reports, "reports folder") if args.reports else None)


def served_tables(settings: Settings) -> list[str]:
    """The identifiers of the tables that the dashboard will show, found as ``ldp mcp`` finds them.

    Raises:
        ConfigError: If a path is not valid, or no table can be shown.
    """
    from local_data_platform.mcp_server.discovery import discover_tables

    discovery = discover_tables(settings.configs, settings.catalogs, allow=list(settings.allow) or None)
    try:
        if not discovery.tables:
            reasons = "; ".join(f"{item['table']}: {item['reason']}" for item in discovery.unavailable) or "none found"
            raise ConfigError(f"no tables to show ({reasons})")
        return [table.identifier for table in discovery.tables]
    finally:
        discovery.close()


def marimo_command(notebook: str | os.PathLike, *, edit: bool = False, headless: bool = False,
                   port: int | None = None) -> list[str]:
    """The command line that starts marimo on a notebook.

    The server listens on :data:`HOST` and nowhere else. ``--no-sandbox`` keeps marimo from asking to build an
    environment of its own, and the update check is off, so that starting the dashboard makes no request to the
    network.
    """
    command = [sys.executable, "-m", "marimo", "edit" if edit else "run", str(notebook), "--host", HOST, "--no-sandbox",
               "--config", "server.skip_update_check=true"]
    if headless:
        command.append("--headless")
    if port is not None:
        command += ["--port", str(port)]
    return command


def edit_copy(folder: str | os.PathLike | None = None) -> Path:
    """The copy of the notebook that ``--edit`` opens. It is made if it is not there, and never overwritten."""
    target = Path(folder if folder is not None else Path.cwd()) / EDIT_COPY
    if not target.exists():
        shutil.copyfile(NOTEBOOK, target)
    return target


def launch(command: Sequence[str], environment: dict[str, str]) -> int:
    """Run marimo until it stops, and give back its exit status.

    Ctrl-C is how a person stops the server. The terminal sends it to marimo too, which shuts down, so the command waits
    for it, stops it if it does not, and exits with 0.
    """
    process = subprocess.Popen(list(command), env=environment)
    try:
        return process.wait()
    except KeyboardInterrupt:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        print("ldp dashboard: stopped", file=sys.stderr)
        return 0


def run_command(args: argparse.Namespace) -> int:
    """Check the extra and the sources, put the settings in the environment and run marimo.

    Returns:
        The exit status: 0 when marimo stops, 1 when the extra is missing, a path is not there, there is no table
        to show or marimo failed to start.
    """
    from .service import SETTINGS_ENV, sources_text

    require_notebook()
    settings = settings_from(args)
    tables = served_tables(settings)
    notebook = edit_copy() if args.edit else NOTEBOOK
    command = marimo_command(notebook, edit=args.edit, headless=args.no_browser, port=args.port)
    environment = {**os.environ, SETTINGS_ENV: settings.to_json()}
    what = "the editable notebook " + str(notebook) if args.edit else "the dashboard"
    print(f"ldp dashboard: {sources_text(settings, len(tables))}, "
          f"{'reports from ' + settings.reports if settings.reports else 'no reports folder'}. Starting {what} on "
          f"{HOST}, Ctrl-C stops it", file=sys.stderr)
    status = launch(command, environment)
    if status != 0:
        print(f"ldp dashboard: marimo stopped with exit status {status}", file=sys.stderr)
        return 1
    return 0


__all__ = ["COMMAND", "EDIT_COPY", "HOST", "NOTEBOOK", "NOTEBOOK_INSTALL_HINT", "add_arguments", "add_cli", "edit_copy",
           "launch", "marimo_command", "missing_packages", "require_notebook", "run_command", "served_tables",
           "settings_from"]
