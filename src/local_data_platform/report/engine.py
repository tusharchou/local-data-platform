"""Running the queries of a report.

A report opens one locked-down DuckDB sandbox, the one ``ldp mcp`` uses, for all of its queries. Each table that the
report can read is registered at the snapshot that the current gold release names for it, or at the snapshot of the
release that was asked for, so the numbers of a report agree with ``ldp metrics query`` and with the exports of the gold
layer. A table that no release holds is read as it is now, and so is every table with ``live``. The queries run with the
report's time zone as the session time zone, so a day is the same day whatever the time zone of the machine. Each query
goes through the statement guard first, and has the row cap and the timeout of the report. A metric block is compiled by
:mod:`local_data_platform.metrics` and runs the same way.

The window is a pair of instants. A query sees it as ``$window_start`` and ``$window_end``, which are wall-clock times
in the time zone of the report, and a day of a ``DATE`` column, a ``TIMESTAMP`` column and a ``TIMESTAMPTZ`` column is
cut at the same moment by ``>= $window_start AND < $window_end``. The parameters that the report declares are bound the
same way. Nothing is ever pasted into the SQL.

:func:`run_queries` runs every query and every metric block, and returns what each gave and every problem that it met,
so that ``ldp report check`` can list them all.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from local_data_platform.exceptions import ConfigError
from local_data_platform.orchestration.windows import get_timezone
from local_data_platform.paths import resolve_path
from local_data_platform.report.formats import metric_format
from local_data_platform.report.spec import BUILTIN_PARAMETERS, Block, Parameter, Report, read_report
from local_data_platform.spec import format_instant, parse_window

if TYPE_CHECKING:
    from pathlib import Path

    import pyarrow as pa

    from local_data_platform.mcp_server.discovery import ExposedTable
    from local_data_platform.mcp_server.sandbox import DuckDBSandbox

_INTEGER = re.compile(r"[+-]?[0-9]+")
_NUMBER = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")


# --------------------------------------------------------------------------- what a run gives


@dataclass(frozen=True)
class TableRead:
    """Which version of a table the queries of a report read.

    Attributes:
        table: ``<namespace>.<table>``.
        release: The release that the table was read as, or ``None`` when it was read as it is now.
        snapshot_id: The snapshot that was read, or ``None`` for a table that has none yet.
        source: Which version, in words, such as ``release r3, cut 2026-10-20 08:07:55 UTC``.
        pipeline: The identifier of the config that writes the table, or ``None`` when no config that was found does.
    """

    table: str
    release: str | None
    snapshot_id: int | None
    source: str
    pipeline: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The read as JSON-serialisable primitives."""
        return {"table": self.table, "release": self.release, "snapshot_id": self.snapshot_id, "source": self.source,
                "pipeline": self.pipeline}


@dataclass
class QueryRun:
    """What one query, or one metric block, did.

    Attributes:
        name: The name of the query, or ``blocks[N]`` for a metric block.
        kind: ``query`` for a query of the report, ``metric`` for a block that computes a metric.
        rows: How many rows it returned, after the row cap.
        columns: The names of its columns.
        duration_ms: How long it took.
        truncated: Whether it had more rows than the row cap, which cut them.
        tables: The tables it reads.
        parameters: The names of the parameters it uses.
        sql: The SQL that ran.
        table: The rows, or ``None`` when the query did not run.
        problem: Why it did not run, as a sentence that starts with a JSON path, or ``None``.
        formats: The format that writes each metric column of a metric block, by column, as a format string.
        columns_info: What a metric block says about its columns, each ``{"name", "kind", "format", "description"}``.
    """

    name: str
    kind: str
    rows: int = 0
    columns: tuple[str, ...] = ()
    duration_ms: float = 0.0
    truncated: bool = False
    tables: tuple[str, ...] = ()
    parameters: tuple[str, ...] = ()
    sql: str = ""
    table: pa.Table | None = None
    problem: str | None = None
    formats: dict[str, str] = field(default_factory=dict)
    columns_info: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        """Whether it ran."""
        return self.problem is None

    def to_dict(self) -> dict[str, Any]:
        """What it did as JSON-serialisable primitives, without the rows."""
        return {"name": self.name, "kind": self.kind, "ok": self.ok, "rows": self.rows, "columns": list(self.columns),
                "duration_ms": self.duration_ms, "truncated": self.truncated, "tables": list(self.tables),
                "parameters": list(self.parameters), "sql": self.sql, "problem": self.problem}


@dataclass
class ReportRun:
    """What running every query of a report gave.

    Attributes:
        report: The report.
        window: The window as UTC instants, or ``None`` when none was given.
        parameters: The value of each parameter that a query may use, as it was bound.
        release: The release that was asked for, or ``None`` for the current one.
        live: Whether the tables were read as they are now.
        queries: What each query did, in the order of the report, and then each metric block, by name.
        reads: The version of each table that a query reads.
        unavailable: The tables that were found but cannot be read, each ``{"table", "reason", "source"}``.
        problems: Every problem, each starting with a JSON path. Empty when everything ran.
        duration_ms: How long the whole run took.
        row_cap: The row cap that the queries ran with.
        views: What each block shows, from :func:`local_data_platform.report.view.resolve`, for the blocks that can
            be shown.
        health: The data health of the tables that the queries read, from
            :func:`local_data_platform.report.health.collect`, or ``None`` when it was not worked out. A build works it
            out and ``ldp report check`` does not.
    """

    report: Report
    window: tuple[dt.datetime, dt.datetime] | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    release: str | None = None
    live: bool = False
    queries: dict[str, QueryRun] = field(default_factory=dict)
    reads: dict[str, TableRead] = field(default_factory=dict)
    unavailable: list[dict[str, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    row_cap: int = 0
    views: list[Any] = field(default_factory=list)
    health: Any = None

    @property
    def ok(self) -> bool:
        """Whether every query ran and nothing else is wrong."""
        return not self.problems

    @property
    def tables(self) -> dict[str, pa.Table]:
        """The rows of each query that ran, by name."""
        return {name: item.table for name, item in self.queries.items() if item.table is not None}

    def to_dict(self) -> dict[str, Any]:
        """The run as JSON-serialisable primitives, without the rows."""
        return {
            "report": self.report.identifier, "ok": self.ok,
            "window": [format_instant(item) for item in self.window] if self.window else None,
            "timezone": self.report.timezone,
            "parameters": {name: value.isoformat() if isinstance(value, (dt.date, dt.datetime)) else value
                           for name, value in self.parameters.items()},
            "release": self.release, "live": self.live, "queries": [item.to_dict() for item in self.queries.values()],
            "tables": [item.to_dict() for item in self.reads.values()], "unavailable": self.unavailable,
            "problems": self.problems, "duration_ms": self.duration_ms}


@dataclass
class Check:
    """What ``ldp report check`` found for one report file.

    Attributes:
        file: The report file.
        report: The report, or ``None`` when the file could not be read as one.
        problems: Every problem, each starting with a JSON path. Empty when the report is valid and every query ran.
        run: The run, or ``None`` when the file could not be read or the run could not start.
    """

    file: Path
    report: Report | None
    problems: list[str]
    run: ReportRun | None = None

    @property
    def ok(self) -> bool:
        """Whether the file is a valid report and every query ran."""
        return not self.problems


# --------------------------------------------------------------------------- the window and the parameters


def wall_clock(moment: dt.datetime, zone: dt.tzinfo) -> dt.datetime:
    """An instant as the wall-clock time of ``zone``, with no time zone, which is how a query sees the window."""
    return moment.astimezone(zone).replace(tzinfo=None)


def report_window(report: Report, window: Any, *,
                  now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime] | None:
    """The window of a run as two UTC instants.

    It is :func:`local_data_platform.spec.parse_window` with the time zone of the report, so ``yesterday``,
    ``last-week`` and ``last-month`` count their days in that zone, and a window that is written with dates is read as
    UTC, as it is for ``ldp run``.

    Raises:
        ConfigError: If the window is malformed.
    """
    return parse_window(window, timezone=get_timezone(report.timezone), now=now)


def coerce_parameter(parameter: Parameter, value: Any) -> Any:
    """A value of a declared parameter, from the text of ``--param`` or from Python, as its type.

    Raises:
        ConfigError: If the value is not of the type of the parameter.
    """
    name, kind = parameter.name, parameter.type
    text = value.strip() if isinstance(value, str) else value
    nice = {"string": "text", "integer": "a whole number", "number": "a number", "boolean": "true or false",
            "date": "a day written YYYY-MM-DD"}[kind]
    bad = ConfigError(f"the parameter {name!r} has to be {nice}, got {value!r}")
    if kind == "string":
        if isinstance(value, str):
            return value
        raise bad
    if kind == "integer":
        if isinstance(text, bool):
            raise bad
        if isinstance(text, int):
            return text
        if isinstance(text, str) and _INTEGER.fullmatch(text):
            return int(text)
        raise bad
    if kind == "number":
        if isinstance(text, bool):
            raise bad
        if isinstance(text, (int, float)):
            return float(text)
        if isinstance(text, str) and _NUMBER.fullmatch(text):
            return float(text)
        raise bad
    if kind == "boolean":
        if isinstance(text, bool):
            return text
        if isinstance(text, str) and text.lower() in ("true", "false"):
            return text.lower() == "true"
        raise bad
    if isinstance(text, dt.datetime):
        raise bad
    if isinstance(text, dt.date):
        return text
    if isinstance(text, str):
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            pass
    raise bad


def parameter_values(report: Report, given: Mapping[str, Any] | None, bounds: tuple[dt.datetime, dt.datetime] | None,
                     zone: dt.tzinfo) -> dict[str, Any]:
    """The value of every parameter a query may use: the window, and what the report declares.

    Args:
        report: The report.
        given: The values that the person gave, by name, as text or as the type of the parameter.
        bounds: The window as UTC instants, or ``None``.
        zone: The time zone of the report.

    Returns:
        ``window_start`` and ``window_end`` as wall-clock times of ``zone`` when there is a window, and each declared
        parameter, from ``given`` or else from its default.

    Raises:
        ConfigError: If a value is not of its type, a name is not declared, or a parameter has no default and no value.
            The message lists every problem.
    """
    given = dict(given or {})
    problems: list[str] = []
    values: dict[str, Any] = {}
    if bounds is not None:
        values["window_start"], values["window_end"] = wall_clock(bounds[0], zone), wall_clock(bounds[1], zone)
    declared = [item.name for item in report.parameters]
    for name in given:
        if name in BUILTIN_PARAMETERS:
            problems.append(f"{name} is set from the window, so it cannot be given as a parameter")
        elif name not in declared:
            problems.append(f"the report has no parameter {name!r}. It declares {declared or 'none'}")
    for parameter in report.parameters:
        if parameter.name in given:
            try:
                values[parameter.name] = coerce_parameter(parameter, given[parameter.name])
            except ConfigError as error:
                problems.append(str(error))
        elif parameter.required:
            problems.append(f"the parameter {parameter.name!r} has no default, so give it with --param "
                            f"{parameter.name}=VALUE")
        else:
            values[parameter.name] = coerce_parameter(parameter, parameter.default)
    if problems:
        raise ConfigError(problems[0] if len(problems) == 1 else "\n".join(
            [f"{len(problems)} problems with the parameters:", *[f"  - {item}" for item in problems]]))
    return values


# --------------------------------------------------------------------------- the tables


def release_for(exposed: ExposedTable, release: str | None, live: bool) -> tuple[Any, int | None]:
    """The release that a table is read as, and the snapshot that the release names for it.

    The release is the current one of the namespace of the table, or ``release`` when one is named. A table with no
    local warehouse has no release, and neither has a namespace that has none yet, and ``live`` reads no release at all.

    Raises:
        ReleaseError: If ``release`` is not a release of a namespace that has some.
    """
    from local_data_platform.gold.release import ReleaseScope, release_in, releases_in, snapshot_for

    warehouse = exposed.catalog.warehouse
    if live or warehouse is None:
        return None, None
    scope = ReleaseScope(warehouse, ".".join(exposed.namespace))
    known = releases_in(scope)
    if release is None:
        chosen = known[0] if known else None
    elif not known:
        chosen = None
    else:
        chosen = release_in(scope, release)
    return chosen, snapshot_for(exposed.identifier, chosen)


def _read_versions(run: ReportRun, sandbox: DuckDBSandbox, tables: Sequence[ExposedTable]) -> dict[str, TableRead]:
    """Put each table at its version, and say which, adding a problem for a release that cannot be read."""
    from local_data_platform.gold.release import ReleaseError
    from local_data_platform.mcp_server.errors import ToolError
    from local_data_platform.metrics.runner import describe_source

    versions: dict[str, TableRead] = {}
    refused: set[str] = set()
    for exposed in tables:
        identifier = exposed.identifier
        if identifier not in sandbox.identifiers:
            continue  # the sandbox could not register it, and said why in the log
        try:
            chosen, snapshot = release_for(exposed, run.release, run.live)
        except ReleaseError as error:
            if str(error) not in refused:
                refused.add(str(error))
                run.problems.append(f"release: {error}")
            continue
        if snapshot is not None:
            try:
                sandbox.read_as_of(identifier, snapshot)
            except ToolError as error:
                run.problems.append(f"release: {chosen.name} names a snapshot of {identifier} that cannot be read. "
                                    f"{error}")
                continue
        versions[identifier] = TableRead(
            table=identifier, release=chosen.name if snapshot is not None else None,
            snapshot_id=sandbox.snapshot_of(identifier),
            source=describe_source(chosen, snapshot is not None, identifier, run.live), pipeline=exposed.pipeline)
    return versions


# --------------------------------------------------------------------------- the queries


def _parameter_problem(used: Sequence[str], run: ReportRun) -> str | None:
    """Why a query cannot run with the parameters that it uses, or ``None``."""
    positional = [name for name in used if name.isdigit()]
    if positional:
        return ("uses a positional parameter, $1 or ?, and a report takes named ones such as $window_start. A value is "
                "never pasted into the SQL")
    declared = [item.name for item in run.report.parameters]
    windowless = [name for name in BUILTIN_PARAMETERS if name in used and name not in run.parameters]
    if windowless:
        return (f"uses {' and '.join('$' + name for name in windowless)}, and no window was given. Pass --window "
                "START/END, or yesterday")
    for name in used:
        if name not in run.parameters:
            return (f"uses ${name}, which the report does not declare. It can use "
                    f"{[*BUILTIN_PARAMETERS, *declared]}")
    return None


def _first_line(error: BaseException) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


def _failure(error: BaseException, run: ReportRun, sandbox: DuckDBSandbox) -> str:
    """The sentence for a query that did not run, with the tables that it could have read, and the ones it cannot."""
    message = _first_line(error).rstrip(".!")
    if "does not exist" in message:
        message += f". The tables of this report are {sandbox.identifiers}"
        if run.unavailable:
            message += (". These tables were found and cannot be read: "
                        + "; ".join(f"{item['table']} ({item['reason']})" for item in run.unavailable))
    return message


def _run_query(run: ReportRun, sandbox: DuckDBSandbox, name: str, sql: str, cap: int, limit: float) -> QueryRun:
    from local_data_platform.mcp_server.errors import ToolError

    item = QueryRun(name=name, kind="query", sql=sql)
    where = f"metadata.queries.{name}.sql"
    try:
        used = sandbox.parameters(sql)
        problem = _parameter_problem(used, run)
        if problem:
            item.problem = f"{where}: {problem}"
            return item
        item.parameters = used
        found = sandbox.tables_in(sql)
        item.tables = tuple(found) if found is not None else tuple(sandbox.identifiers)
        bound = {parameter: run.parameters[parameter] for parameter in used}
        result = sandbox.query(sql, max_rows=cap, timeout_s=limit, params=bound or None)
    except ToolError as error:
        item.problem = f"{where}: {_failure(error, run, sandbox)}"
        return item
    item.rows, item.columns = result.table.num_rows, tuple(result.table.column_names)
    item.duration_ms, item.truncated, item.table = result.duration_ms, result.truncated, result.table
    return item


def _days(bounds: tuple[dt.datetime, dt.datetime] | None, zone: dt.tzinfo) -> tuple[dt.date | None, dt.date | None]:
    """The first and the last day that a window touches in ``zone``, which is what a metric reads."""
    if bounds is None:
        return None, None
    return (bounds[0].astimezone(zone).date(),
            (bounds[1].astimezone(zone) - dt.timedelta(microseconds=1)).date())


def _run_metric(run: ReportRun, sandbox: DuckDBSandbox, block: Block, definitions: Any, trouble: str | None, cap: int,
                limit: float, zone: dt.tzinfo) -> QueryRun:
    from local_data_platform.mcp_server.errors import ToolError
    from local_data_platform.metrics.compiler import compile_query, select_metrics
    from local_data_platform.metrics.load import resolve_table

    item = QueryRun(name=f"blocks[{block.index}]", kind="metric")
    where = f"metadata.blocks[{block.index}].metric"
    try:
        if trouble is not None:
            raise ConfigError(trouble)
        if definitions is None:
            raise ConfigError("the config folders of metadata.tables have no metrics file. A metrics file has "
                              "\"kind\": \"Metrics\"")
        model, _selected = select_metrics(definitions, [block.metric])
        table = resolve_table(model.table, sandbox.identifiers)
        table_sql = sandbox.sql_name(table)
        first, last = _days(run.window, zone)
        compiled = compile_query(definitions, [block.metric], table=table, types=sandbox.columns(table_sql),
                                 by=list(block.by) or None, grain=block.grain, filters=list(block.filters) or None,
                                 start=first, end=last, table_sql=table_sql)
        item.sql, item.tables = compiled.sql, (table,)
        item.formats = {column.name: written for column in compiled.columns
                        if column.kind == "metric" and (written := metric_format(column.format)) is not None}
        item.columns_info = tuple(column.to_dict() for column in compiled.columns)
        result = sandbox.query(compiled.sql, max_rows=cap, timeout_s=limit, params=compiled.parameters)
    except (ConfigError, ToolError) as error:
        item.problem = f"{where}: {_failure(error, run, sandbox)}"
        return item
    item.rows, item.columns = result.table.num_rows, tuple(result.table.column_names)
    item.duration_ms, item.truncated, item.table = result.duration_ms, result.truncated, result.table
    return item


def _read_definitions(report: Report) -> tuple[Any, str | None]:
    """The metrics of the config folders of a report, or ``None`` and the reason they could not be read."""
    from local_data_platform.metrics.load import load_all

    try:
        return load_all([resolve_path(item, report.base_dir) for item in report.tables.config]), None
    except ConfigError as error:
        lines = [line.strip().removeprefix("- ") for line in str(error).splitlines() if line.strip()]
        return None, lines[0] + (": " + "; ".join(lines[1:]) if lines[1:] else "")


def run_queries(report: Report, window: Any = None, *, params: Mapping[str, Any] | None = None,
                release: str | None = None, live: bool = False, row_cap: int | None = None,
                timeout_s: float | None = None, now: dt.datetime | None = None) -> ReportRun:
    """Run every query and every metric block of a report, each at the version of its tables that a release names.

    Args:
        report: The report, from :func:`~local_data_platform.report.spec.read_report`.
        window: The window, in any form :func:`~local_data_platform.spec.parse_window` takes: ``START/END``, a pair,
            or ``yesterday``, ``last-week`` or ``last-month`` in the time zone of the report. ``None`` for no window,
            which the queries that use ``$window_start`` cannot do without.
        params: The values of the declared parameters, by name, as text or as their type.
        release: Read this release of each namespace, such as ``r2``, and not the current one.
        live: Read the tables as they are now, and no release.
        row_cap: How many rows a query may return. The ``row_cap`` of the report by default.
        timeout_s: How many seconds a query may run. The ``timeout_s`` of the report by default.
        now: The current time, for the shortcuts.

    Returns:
        The :class:`ReportRun`. A query that fails, a table that cannot be read and a metric that does not fit are in
        ``problems`` and do not stop the other queries.

    Raises:
        ConfigError: If the window is malformed, the parameters do not fit, or ``release`` and ``live`` are both given.
        EngineNotFound: If ``duckdb`` is not installed.
    """
    from local_data_platform.mcp_server.discovery import discover_tables
    from local_data_platform.mcp_server.sandbox import DuckDBSandbox

    if release is not None and live:
        raise ConfigError("pass either a release or live, not both")
    zone = get_timezone(report.timezone)
    bounds = report_window(report, window, now=now)
    values = parameter_values(report, params, bounds, zone)
    cap = row_cap if row_cap is not None else report.row_cap
    limit = timeout_s if timeout_s is not None else report.timeout_s
    started = time.perf_counter()
    run = ReportRun(report=report, window=bounds, parameters=values, release=release, live=live, row_cap=cap)
    try:
        discovery = discover_tables([resolve_path(item, report.base_dir) for item in report.tables.config],
                                    [resolve_path(item, report.base_dir) for item in report.tables.catalog],
                                    allow=list(report.tables.allow) if report.tables.allow else None)
    except ConfigError as error:
        run.problems.append(f"metadata.tables: {error}")
        run.duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return run
    try:
        seen: set[tuple[str, str]] = set()
        for item in discovery.unavailable:  # a config that has the table as its source and its target lists it twice
            if (item["table"], item["reason"]) not in seen:
                seen.add((item["table"], item["reason"]))
                run.unavailable.append(item)
        if not discovery.tables:
            reasons = "; ".join(f"{item['table']}: {item['reason']}" for item in discovery.unavailable) or "none found"
            run.problems.append(f"metadata.tables: there is no table to read ({reasons})")
            return run
        try:
            sandbox = DuckDBSandbox(discovery.tables, timezone=report.timezone)
        except ValueError as error:
            run.problems.append(f"metadata.timezone: {error}")
            return run
        with sandbox:
            versions = _read_versions(run, sandbox, discovery.tables)
            for query in report.queries:
                run.queries[query.name] = _run_query(run, sandbox, query.name, query.sql or "", cap, limit)
            if report.metric_blocks:
                definitions, trouble = _read_definitions(report)
                for number, block in enumerate(report.metric_blocks):
                    reason = trouble if number == 0 or trouble is None else (
                        "the metrics files could not be read, which the first metric block says")
                    item = _run_metric(run, sandbox, block, definitions, reason, cap, limit, zone)
                    run.queries[item.name] = item
            read = {table for item in run.queries.values() for table in item.tables}
            run.reads = {name: version for name, version in versions.items() if name in read}
        run.problems.extend(item.problem for item in run.queries.values() if item.problem)
        from local_data_platform.report.view import resolve

        run.views, found = resolve(run)  # what each block shows, and a column that a block names and its query lacks
        run.problems.extend(found)
    finally:
        discovery.close()
        run.duration_ms = round((time.perf_counter() - started) * 1000, 3)
    return run


def check_report(path: str | Path, window: Any = None, *, params: Mapping[str, Any] | None = None,
                 release: str | None = None, live: bool = False, row_cap: int | None = None,
                 timeout_s: float | None = None, now: dt.datetime | None = None) -> Check:
    """Read a report file, check it, and run every query and metric block.

    It does what ``ldp report check`` does for one file, and takes what :func:`run_queries` takes. Nothing is raised for
    a problem of the file or of a query: each is in :attr:`Check.problems`, so that every one can be listed.

    Returns:
        The :class:`Check`.

    Raises:
        EngineNotFound: If ``duckdb`` is not installed.
    """
    file = resolve_path(path)
    report, problems = read_report(file)
    if report is None:
        return Check(file=file, report=None, problems=problems)
    try:
        run = run_queries(report, window, params=params, release=release, live=live, row_cap=row_cap,
                          timeout_s=timeout_s, now=now)
    except ConfigError as error:
        return Check(file=file, report=report, problems=[str(error)])
    return Check(file=file, report=report, problems=list(run.problems), run=run)


__all__ = [
    "Check",
    "QueryRun",
    "ReportRun",
    "TableRead",
    "check_report",
    "coerce_parameter",
    "parameter_values",
    "release_for",
    "report_window",
    "run_queries",
    "wall_clock",
]
