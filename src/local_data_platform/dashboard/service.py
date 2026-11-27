"""What the pages of ``ldp dashboard`` show, as plain Python.

The notebook in :mod:`local_data_platform.dashboard.app` only lays these out. Everything that has to be right is here,
where it needs neither marimo nor Altair, so a test can run it in every job. A :class:`Dashboard` has one page each.

* **Tables**: :meth:`Dashboard.tables` and :meth:`Dashboard.table_view`, the schema, the first rows and the
  snapshots of a table.
* **SQL**: :meth:`Dashboard.run_sql`, one query, and :func:`chart_axes` and :func:`chart_rows`, which pick the
  columns of a quick bar or line chart.
* **Reports**: :meth:`Dashboard.reports`, :meth:`Dashboard.preview_report` and :meth:`Dashboard.write_report`,
  which are ``ldp report build`` for a report and a window.
* **Health**: :meth:`Dashboard.health_rows`, the rows of ``ldp status``.

Every query that a person writes, and every call that lists, describes, samples or reads the history of a table, is a
call of :class:`~local_data_platform.mcp_server.tools.LakeTools`. The tools are built with the tool prefix
:data:`TOOL_PREFIX`, so the statement guard, the locked-down DuckDB, the row cap and the timeout apply to each of
them, and each one is an audit record whose tool starts with ``dashboard.``, such as ``dashboard.query``. A report is
read by the engine of ``ldp report build``, which uses the same sandbox, the same guard, and the row cap and the
timeout of the report, with the queries of the report file and not the text of a person. A report that the Reports
page builds or writes is one more audit record, ``dashboard.report``, so that everything that the page did is in
one log.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_data_platform import events
from local_data_platform.exceptions import ConfigError, LDPError
from local_data_platform.logger import get_logger
from local_data_platform.mcp_server.audit import AuditRecord
from local_data_platform.mcp_server.tools import LakeTools
from local_data_platform.monitoring import status
from local_data_platform.orchestration.windows import SHORTCUTS
from local_data_platform.report.build import ReportBuildError, build_report
from local_data_platform.report.health import ReportHeld
from local_data_platform.report.spec import read_report, report_files

logger = get_logger(__name__)

TOOL_PREFIX = "dashboard."
"""The prefix of the tool name in every audit record that the dashboard makes."""

SETTINGS_ENV = "LDP_DASHBOARD_SETTINGS"
"""The environment variable that carries the command line of ``ldp dashboard`` to the notebook, as JSON."""

WINDOWS = tuple(SHORTCUTS)
"""The windows that the Reports page offers by name. A window of two instants, ``START/END``, is typed."""

SAMPLE_ROWS = 10
"""How many rows of a table the Tables page shows."""

HISTORY_ROWS = 20
"""How many snapshots of a table the Tables page lists."""

MAX_SAFE_INTEGER = 2**53
"""A whole number beyond this is shown as text, because the browser reads it as a float and would round it."""


# --------------------------------------------------------------------------- the command line, carried to the notebook


@dataclass(frozen=True)
class Settings:
    """What ``ldp dashboard`` was asked for, which the notebook reads from the environment.

    Attributes:
        configs: The config files or folders of configs whose tables are served and whose pipelines the Health page
            shows.
        catalogs: The catalog spec files whose tables are served.
        allow: The table allowlist, as ``ldp mcp --allow`` takes it. Empty serves every table.
        reports: A report file or a folder of report files, or ``None``.
    """

    configs: tuple[str, ...] = ()
    catalogs: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    reports: str | None = None

    def to_json(self) -> str:
        """The settings as the JSON text that goes into :data:`SETTINGS_ENV`."""
        return json.dumps({"configs": list(self.configs), "catalogs": list(self.catalogs), "allow": list(self.allow),
                           "reports": self.reports})

    @classmethod
    def from_json(cls, text: str) -> Settings:
        """Read the text that :meth:`to_json` wrote.

        Raises:
            ConfigError: If it is not JSON of the right shape.
        """
        try:
            data = json.loads(text)
        except ValueError as error:
            raise ConfigError(f"{SETTINGS_ENV} is not JSON: {error}") from error
        if not isinstance(data, dict):
            raise ConfigError(f"{SETTINGS_ENV} must be a JSON object")
        lists = {}
        for key in ("configs", "catalogs", "allow"):
            value = data.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ConfigError(f"{SETTINGS_ENV} has {key!r}, which must be a list of text")
            lists[key] = tuple(value)
        reports = data.get("reports")
        if reports is not None and not isinstance(reports, str):
            raise ConfigError(f"{SETTINGS_ENV} has 'reports', which must be text or null")
        return cls(configs=lists["configs"], catalogs=lists["catalogs"], allow=lists["allow"], reports=reports)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """The settings that ``ldp dashboard`` put in the environment.

        Raises:
            ConfigError: If there are none, which is the case when the notebook was not started by ``ldp dashboard``.
        """
        text = (os.environ if environ is None else environ).get(SETTINGS_ENV)
        if not text:
            raise ConfigError(f"the dashboard was not told which tables to show. Start it with ldp dashboard --config "
                              f"PATH, which sets {SETTINGS_ENV}")
        return cls.from_json(text)


# --------------------------------------------------------------------------- grids of rows and charts


@dataclass
class Grid:
    """Rows of a result, in the form that a table of the page takes.

    Attributes:
        columns: The names of the columns. A name that is there twice gets ``_2``, ``_3`` and so on.
        types: The type of each column, as Arrow names it.
        rows: One dict for each row, by column name.
        truncated: Whether the rows were cut at the row cap.
        max_rows: The row cap that the result was cut at.
        duration_ms: How long the query took.
    """

    columns: tuple[str, ...]
    types: tuple[str, ...]
    rows: list[dict[str, Any]]
    truncated: bool = False
    max_rows: int | None = None
    duration_ms: float | None = None


def unique_names(names: Iterable[str]) -> list[str]:
    """The names, with ``_2``, ``_3`` and so on added to one that is there again, so that a row can be a dict."""
    seen: dict[str, int] = {}
    out = []
    for name in names:
        count = seen.get(name, 0) + 1
        seen[name] = count
        out.append(name if count == 1 else f"{name}_{count}")
    return out


def shown(value: Any) -> Any:
    """A value of a row as the page shows it. A whole number that a browser would round is text."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_SAFE_INTEGER:
        return str(value)
    return value


def grid_from(payload: Mapping[str, Any]) -> Grid:
    """A :class:`Grid` from the payload of a ``query`` or ``sample_rows`` call of :class:`LakeTools`."""
    columns = payload.get("columns") or []
    names = unique_names(str(item["name"]) for item in columns)
    rows = [{name: shown(value) for name, value in zip(names, row)} for row in payload.get("rows") or []]
    return Grid(columns=tuple(names), types=tuple(str(item["type"]) for item in columns), rows=rows,
                truncated=bool(payload.get("truncated")), max_rows=payload.get("max_rows"),
                duration_ms=payload.get("duration_ms"))


_NUMBER = ("int", "uint", "float", "halffloat", "double", "decimal")
_TIME = ("date", "timestamp", "time")


def column_kind(type_name: str) -> str:
    """``quantitative``, ``temporal`` or ``nominal``, which is how a chart reads a column of this Arrow type."""
    name = type_name.lower()
    if name.startswith(_NUMBER):
        return "quantitative"
    if name.startswith(_TIME):
        return "temporal"
    return "nominal"


@dataclass(frozen=True)
class ChartAxes:
    """The columns that a quick chart of a result can use, and what it starts with.

    Attributes:
        x: The columns that can be on the horizontal axis, which is every column.
        y: The columns that can be on the vertical axis, which are the numbers.
        kind: The chart to start with, ``line`` when the first column of the axis is a time and ``bar`` otherwise.
        x_default: The column to start with on the horizontal axis.
        y_default: The column to start with on the vertical axis, or ``None`` when there is no number to chart.
    """

    x: tuple[str, ...]
    y: tuple[str, ...]
    kind: str
    x_default: str | None
    y_default: str | None


def chart_axes(grid: Grid) -> ChartAxes:
    """Which columns of a result a bar or line chart can use, and which it starts with."""
    kinds = dict(zip(grid.columns, (column_kind(item) for item in grid.types)))
    y = tuple(name for name in grid.columns if kinds[name] == "quantitative")
    wanted = [name for name in grid.columns if kinds[name] != "quantitative"] or list(grid.columns)
    x_default = wanted[0] if wanted else None
    y_default = next((name for name in y if name != x_default), None)
    kind = "line" if x_default is not None and kinds[x_default] == "temporal" else "bar"
    return ChartAxes(x=grid.columns, y=y, kind=kind, x_default=x_default, y_default=y_default)


def chart_x_type(grid: Grid, x: str, kind: str) -> str:
    """How a chart reads the horizontal axis, ``temporal``, ``nominal`` or ``quantitative``, and ``ordinal`` for numbers
    under bars, so that each value gets a bar of its own."""
    types = dict(zip(grid.columns, grid.types))
    column = column_kind(types.get(x, ""))
    return "ordinal" if column == "quantitative" and kind == "bar" else column


def _number(value: Any) -> float | None:
    """A number of a row as a float, or ``None`` for a NULL or a text that is not a number (``NaN`` is text there)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def chart_rows(grid: Grid, x: str, y: str) -> list[dict[str, Any]]:
    """The rows of a chart, ``{"x", "y"}`` for each row of the result that has a number in the column ``y``.

    The chart names its fields ``x`` and ``y``, so that a column with a dot or a bracket in its name needs no escaping,
    and it shows the names of the columns as the titles of its axes.

    Raises:
        ValueError: If ``x`` or ``y`` is not a column of the result.
    """
    for column in (x, y):
        if column not in grid.columns:
            raise ValueError(f"the result has no column {column!r}; its columns are {list(grid.columns)}")
    rows = []
    for row in grid.rows:
        number = _number(row.get(y))
        if number is not None:
            rows.append({"x": row.get(x), "y": number})
    return rows


# --------------------------------------------------------------------------- what each page gives


@dataclass
class TableView:
    """What the Tables page shows for one table.

    Attributes:
        table: The identifier of the table.
        facts: What the table says about itself, as label and value, for the lines under the title.
        schema: One dict for each column, with ``name``, ``type``, ``required`` and ``doc``.
        sample: The first rows, or ``None`` when they could not be read.
        history: One dict for each snapshot, the newest first.
        problems: What could not be read, each in a line.
    """

    table: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    schema: list[dict[str, Any]] = field(default_factory=list)
    sample: Grid | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class SqlRun:
    """What the SQL page got for a query.

    Attributes:
        sql: The query.
        status: ``ok``, or what the audit record says when it did not run, which is ``rejected`` for a statement
            that the guard refuses, ``denied`` for a table that is not served, ``timeout`` and ``error``.
        grid: The rows, when it ran.
        message: Why it did not run, in one line of words.
    """

    sql: str
    status: str
    grid: Grid | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the query ran."""
        return self.status == "ok"


@dataclass
class ReportItem:
    """A report file that the Reports page lists.

    Attributes:
        path: The file.
        identifier: The id of the report, or ``None`` when the file has problems.
        title: What the report is called, or ``None`` when the file has problems.
        problems: What is wrong with the file, each in a line. A file that has some cannot be built.
    """

    path: str
    identifier: str | None
    title: str | None
    problems: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """The name of the report in the list: its title and its file, and a mark when it has problems."""
        name = Path(self.path).name
        if self.title and self.title != self.identifier:
            text = f"{self.title} ({name})"
        else:
            text = self.identifier or name
        return f"{text} - has problems" if self.problems else text


@dataclass
class ReportResult:
    """What building or writing a report gave.

    Attributes:
        path: The report file.
        window: The window that was asked for, or ``None``.
        html: The page, when the report was built.
        output: Where the page was written, or ``None`` for a build that only shows it.
        bytes: The size of the page, or ``None``.
        sha256: The digest of the page, or ``None``.
        facts: The title, the window, the tables that were read with their release, and the time of the build.
        problems: Why the report was not built, each in a line, or what is wrong with its file.
        held: Whether a data gate held the report back, which is a stale table under ``metadata.freshness`` that says
            ``block``. ``problems`` says which.
    """

    path: str
    window: str | None
    html: str | None = None
    output: str | None = None
    bytes: int | None = None
    sha256: str | None = None
    facts: list[tuple[str, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    held: bool = False

    @property
    def ok(self) -> bool:
        """Whether the report was built."""
        return self.html is not None and not self.problems


@dataclass
class HealthPage:
    """What the Health page shows.

    Attributes:
        rows: One dict for each pipeline, the worst first, with ``health``, then the columns of ``ldp status``, and
            ``problem`` for a config that could not be read.
        problems: A config or a folder that could not be read at all, each in a line.
        counts: How many pipelines there are of each health, for the line above the table.
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


class Dashboard:
    """The pages of the dashboard over one :class:`LakeTools`.

    Build it with :meth:`open`, and close it with :meth:`close` or as a context manager.

    Args:
        settings: What ``ldp dashboard`` was asked for.
        tools: The tools that every query goes through. They should have :data:`TOOL_PREFIX` as their tool prefix, which
            :meth:`open` sets.
    """

    def __init__(self, settings: Settings, tools: LakeTools):
        self.settings = settings
        self.tools = tools
        self._listed: list[dict[str, Any]] | None = None

    @classmethod
    def open(cls, settings: Settings, **kwargs: Any) -> Dashboard:
        """Build the tools for the settings and a dashboard over them.

        The audit records go to the JSONL file of ``ldp mcp`` and not to ``_ldp.audit``, because the Iceberg sink
        appends its records when the tools are closed, and a page that is closed in the browser leaves no moment for
        that. Nothing that the dashboard does writes to the lake.

        Args:
            settings: What ``ldp dashboard`` was asked for.
            **kwargs: Passed to :meth:`LakeTools.from_sources`, such as ``audit_path`` or ``max_rows``.

        Raises:
            ConfigError: If a path is not there or no table can be served.
        """
        kwargs.setdefault("iceberg_audit", False)
        tools = LakeTools.from_sources(settings.configs, settings.catalogs, allow=list(settings.allow) or None,
                                       tool_prefix=TOOL_PREFIX, **kwargs)
        if not tools.tables:
            reasons = "; ".join(f"{item['table']}: {item['reason']}" for item in tools.unavailable) or "none found"
            tools.close()
            raise ConfigError(f"no tables to show ({reasons})")
        return cls(settings, tools)

    def close(self) -> None:
        """Close the tools. It is safe to call it more than once."""
        self.tools.close()

    def __enter__(self) -> Dashboard:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        outcome = self.tools.call(tool, arguments)
        if not outcome.ok:
            raise LDPError(outcome.error or f"{tool} failed")
        return outcome.data

    @property
    def limits(self) -> dict[str, Any]:
        """The row cap and the timeout that every query has, which the pages say."""
        return {"max_rows": self.tools.max_rows, "timeout_s": self.tools.timeout_s}

    # ------------------------------------------------------------------ Tables

    def tables(self, refresh: bool = False) -> list[dict[str, Any]]:
        """The served tables, as ``list_tables`` gives them, with what each says about itself.

        The list is read once and kept, because it opens every table, and ``refresh`` reads it again.

        Raises:
            LDPError: If the call failed.
        """
        if self._listed is None or refresh:
            self._listed = self._call("list_tables")["tables"]
        return self._listed

    def table_view(self, table: str) -> TableView:
        """The schema, the first rows and the snapshots of a table.

        What cannot be read is a problem of the view and not an error.
        """
        view = TableView(table=table)
        listed = next((item for item in self.tables() if item["table"] == table), None)
        try:
            detail = self._call("describe_table", table=table)
        except LDPError as error:
            view.problems.append(f"describe_table: {error}")
            detail = None
        if detail is not None:
            view.schema = [{"name": item["name"], "type": item["type"], "required": item["required"],
                            "doc": item.get("doc") or ""} for item in detail["schema"]["fields"]]
            contract = detail.get("contract") or {}
            documented = contract.get("columns") or {}
            for row in view.schema:
                row["doc"] = row["doc"] or documented.get(row["name"], "")
            view.facts = self._facts(listed or {}, detail)
        elif listed is not None:
            view.facts = self._facts(listed, None)
        try:
            view.sample = grid_from(self._call("sample_rows", table=table, n=SAMPLE_ROWS))
        except LDPError as error:
            view.problems.append(f"sample_rows: {error}")
        try:
            history = self._call("table_history", table=table, limit=HISTORY_ROWS)
            view.history = [{
                "committed_at": item.get("committed_at") or "-", "snapshot_id": str(item["snapshot_id"]),
                "operation": item.get("operation") or "-", "added_records": item.get("added_records"),
                "deleted_records": item.get("deleted_records"), "total_records": item.get("total_records"),
                "current": "yes" if item.get("is_current") else ""} for item in history["history"]]
        except LDPError as error:
            view.problems.append(f"table_history: {error}")
        return view

    @staticmethod
    def _facts(listed: Mapping[str, Any], detail: Mapping[str, Any] | None) -> list[tuple[str, str]]:
        facts = [("Layer", listed.get("layer")), ("Owner", listed.get("owner")),
                 ("Meaning", listed.get("description")), ("One row is", listed.get("grain")),
                 ("Rows", listed.get("row_count")), ("Last updated", listed.get("last_updated")),
                 ("Name in SQL", listed.get("sql_name")), ("Read as", listed.get("access"))]
        if detail is not None:
            facts += [("Snapshots", detail.get("snapshot_count")), ("Location", detail.get("location"))]
        return [(label, str(value)) for label, value in facts if value not in (None, "")]

    # ------------------------------------------------------------------ SQL

    def default_sql(self) -> str:
        """The query that the SQL page starts with, the first rows of the first table."""
        first = next(iter(self.tables()), None)
        if first is None:
            return "SELECT 1"
        return f"SELECT * FROM {first.get('sql_name') or first['table']} LIMIT 10"

    def run_sql(self, sql: str) -> SqlRun:
        """Run one query as ``ldp mcp`` runs it, with the guard, the row cap and the timeout, and audit it.

        Never raises: a query that did not run is a :class:`SqlRun` that says why.
        """
        text = (sql or "").strip()
        if not text:
            return SqlRun(sql or "", "error", message="there is no query to run")
        outcome = self.tools.call("query", {"sql": text, "max_rows": self.tools.max_rows})
        if not outcome.ok:
            return SqlRun(text, outcome.status, message=outcome.error)
        return SqlRun(text, "ok", grid=grid_from(outcome.data))

    # ------------------------------------------------------------------ Reports

    def reports(self) -> list[ReportItem]:
        """The report files of ``--reports``, with the problems of each. Nothing is run.

        Raises:
            ConfigError: If the folder or file is not there.
        """
        if not self.settings.reports:
            return []
        items = []
        for file in report_files(self.settings.reports):
            report, problems = read_report(file)
            items.append(ReportItem(str(file), report.identifier if report else None, report.title if report else None,
                                    list(problems)))
        return items

    def preview_report(self, path: str, window: str | None = None) -> ReportResult:
        """Build a report for a window and give the page, as ``ldp report build`` does, and write nothing.

        The build records no run event, because a preview that a person looks at is not a build of the report.
        """
        return self._build(path, window, out=None)

    def write_report(self, path: str, window: str | None, out: str | os.PathLike) -> ReportResult:
        """Build a report for a window and write the page to ``out``, as ``ldp report build --out`` does.

        The build is recorded as run events in the sinks of the report, as the command records it. Nothing is
        written when the report has a problem or is held back, and a file that was there stays.
        """
        return self._build(path, window, out=out)

    def _build(self, path: str, window: str | None, out: str | os.PathLike | None) -> ReportResult:
        started = time.perf_counter()
        window = (window or "").strip() or None
        result = ReportResult(path=str(path), window=window)
        report, problems = read_report(path)
        if report is None:
            result.problems = list(problems)
            self._audit_report(result, None, started)
            return result
        try:
            built = build_report(report, window, out=out, fmt="html",
                                 sink=events.NullSink() if out is None else None)
        except ReportHeld as held:
            result.problems = str(held).splitlines()
            result.held = True
        except ReportBuildError as error:
            result.problems = list(error.problems)
        except LDPError as error:
            result.problems = str(error).splitlines() or [type(error).__name__]
        else:
            result.html = built.document
            result.output = built.output
            result.bytes = built.bytes
            result.sha256 = built.sha256
            result.facts = self._report_facts(built)
        self._audit_report(result, report, started)
        return result

    @staticmethod
    def _report_facts(built: Any) -> list[tuple[str, str]]:
        run = built.run
        facts = [("Report", f"{built.report.title} ({built.report.identifier})")]
        if run.window is not None:
            facts.append(("Window", f"{run.window[0].isoformat()} to {run.window[1].isoformat()}"))
        facts.append(("Time zone", built.report.timezone))
        facts += [(f"Read {read.table}", read.source) for read in run.reads.values()]
        facts.append(("Built", f"{built.built_at.isoformat(timespec='seconds')} in {built.duration_ms:.0f} ms"))
        return facts

    def _audit_report(self, result: ReportResult, report: Any, started: float) -> None:
        """Record one build of a report as an audit record that starts with ``dashboard.``."""
        status_text = "ok" if result.ok else "error"
        arguments: dict[str, Any] = {"report": report.identifier if report is not None else Path(result.path).name,
                                     "window": result.window, "written": result.output is not None}
        try:
            self.tools.audit.record(AuditRecord(
                tool=f"{self.tools.tool_prefix}report", status=status_text,
                duration_ms=round((time.perf_counter() - started) * 1000, 3), arguments=arguments,
                error=None if result.ok else "; ".join(result.problems)[:1000]))
        except Exception as error:  # noqa: BLE001 - never fail a page because of the audit, as LakeTools does not
            logger.error("Could not write the audit record: %s", error)

    # ------------------------------------------------------------------ Health

    def health_rows(self) -> HealthPage:
        """The health of every pipeline of the ``--config`` paths, the worst first, as ``ldp status`` shows it."""
        page = HealthPage()
        if not self.settings.configs:
            page.problems.append("the health of a pipeline comes from its config, so start the dashboard with --config")
            return page
        found = []
        for config in self.settings.configs:
            try:
                found.extend(status.rows(config))
            except LDPError as error:
                page.problems.append(f"{config}: {error}")
        found.sort(key=lambda row: (status.HEALTH.index(row.health), row.pipeline))
        for row in found:
            page.rows.append({"health": row.health, **{key: shown(value) for key, value in row.cells().items()},
                              "problem": row.problem or ""})
        for health in status.HEALTH:
            count = sum(1 for row in found if row.health == health)
            if count:
                page.counts[health] = count
        return page


def sources_text(settings: Settings, count: int) -> str:
    """One line for the top of the page, such as ``3 tables from 1 config``."""
    parts = []
    if settings.configs:
        parts.append(f"{len(settings.configs)} config{'s' if len(settings.configs) != 1 else ''}")
    if settings.catalogs:
        parts.append(f"{len(settings.catalogs)} catalog{'s' if len(settings.catalogs) != 1 else ''}")
    return f"{count} table{'s' if count != 1 else ''} from {' and '.join(parts) or 'no source'}"


def overview_rows(tables: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row for each table, for the list at the top of the Tables page, from :meth:`Dashboard.tables`."""
    return [{"table": item["table"], "layer": item.get("layer") or "-", "owner": item.get("owner") or "-",
             "rows": item.get("row_count"), "last updated": item.get("last_updated") or "-",
             "meaning": item.get("description") or "-", "read as": item.get("access") or "-"} for item in tables]


def facts_rows(facts: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    """Facts as the rows of a two column table, which shows any text as it is and needs no escaping."""
    return [{"what": label, "value": value} for label, value in facts]


_IDENTIFIER = re.compile(r"[^A-Za-z0-9_.-]+")


def default_output(item: ReportItem | None) -> str:
    """The file that the Reports page starts with for writing a report, its identifier and ``.html``.

    It is a name with no folder, so the file goes to the folder that the dashboard was started in.
    """
    name = (item.identifier if item is not None and item.identifier else "report")
    return f"{_IDENTIFIER.sub('_', name)}.html"


__all__ = ["ChartAxes", "Dashboard", "Grid", "HISTORY_ROWS", "HealthPage", "ReportItem", "ReportResult", "SAMPLE_ROWS",
           "SETTINGS_ENV", "Settings", "SqlRun", "TOOL_PREFIX", "TableView", "WINDOWS", "chart_axes", "chart_rows",
           "chart_x_type",
           "column_kind", "default_output", "facts_rows", "grid_from", "overview_rows", "shown", "sources_text",
           "unique_names"]
